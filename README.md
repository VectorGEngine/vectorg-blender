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

`manifest.json` identifies the exported model explicitly:

```json
{
  "version": 2,
  "id": "<car_id>",
  "packageVersion": "1",
  "model": "<car_id>.glb",
  "engine": {
    "torqueFactor": 1.0,
    "finalDriveRatio": 5.0,
    "gearRatios": {
      "-1": -3.57,
      "0": 0,
      "1": 4.08
    },
    "idleRPM": 1000,
    "redlineRPM": 7000,
    "revLimit": 7900,
    "maxRPM": 8000,
    "autoBlip": true
  },
  "presets": [
    {
      "id": "default",
      "name": "Default",
      "wheels": {
        "front": {
          "l": {
            "tireType": "medium",
            "pressure": 2.0,
            "camber": -4.0,
            "toe": -0.15,
            "suspensionOffset": 0.0,
            "suspensionStiffness": 80.0,
            "dampingRelaxation": 2.6,
            "dampingCompression": 2.0
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
The Gears section exports the final drive as `engine.finalDriveRatio` and the
individual ratios as `engine.gearRatios`.
The Torque Curve section exports `engine.torqueFactor`, which scales drive and
engine-braking torque before tire-force limits are applied.
The game applies auto blip only when both its gameplay setting and the vehicle's
`engine.autoBlip` capability are enabled.

Wheel object selections, axes, radius, steering behavior, braking, and friction
parameters are shared by every preset. Presets contain tire type, pressure,
camber, toe, suspension offset, suspension stiffness, relaxation damping, and
compression damping. The current game uses the first preset. Tire type is
`soft`, `medium`, or `hard`, with `medium` as the default. Suspension offset is
a signed change in metres to the calculated suspension rest length. Positive
values move the wheel farther down from the mount; negative values move it
toward the mount. The mount position and maximum suspension travel remain
unchanged. Applying the same offset to every wheel raises or lowers the chassis.
Each shared wheel's `spin.gripFactor` multiplies its pressure-derived grip;
`2.0` doubles grip and `0.5` halves it.

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
On Low / Mid / High
Off Low / Mid / High
Limiter
Turbo Flutter
```

Assigned files are copied into `sounds/`. Audio is required by default because the runtime applies fixed engine sample keys every frame. Disable `Require Audio Slots` only when intentionally exporting a visual/physics-only test package.

Only assigned files are written to `manifest.json`; the addon does not emit references to files that are not packaged.

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
for width, local Y for height, and local Z for its normal. It must have one
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

The runtime measures the plane's physical X:Y ratio, including hierarchy
scale. Canvas height is always 1024 pixels and width is calculated from that
ratio. For example, a 2:1 plane creates a 2048 x 1024 canvas.

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
