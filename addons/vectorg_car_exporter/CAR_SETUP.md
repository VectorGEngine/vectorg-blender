# Setting up a new car

Recipe for taking a new car model from a raw import to a tested VectorG package with
this add-on (`View3D > Sidebar > VectorG`). Field-by-field reference: [README](../../README.md).

## Rules

- Present a plan and get the user's go-ahead before every change. Approval covers only that
  change. No unasked saves, exports, installs, commits or reverts.
- Save without backups: `bpy.context.preferences.filepaths.save_version = 0`.
- Never edit body meshes. Never override authored material looks.
- Never change **Package Version** unless asked.
- Before claiming anything, check the reference car and the real code. Verify visual changes with a
  screenshot.
- Code comments and docs never name other games.

## 1. Start

- List every file in the car folder and open the **highest version number**. Do not trust the
  first match.
- Pick the latest bundled reference car of a similar type and read its settings from its
  `.blend`, opened read-only (dump `scene.car_exporter` to JSON).
- Save each step as the next version (`car.04.blend`, `.05`, ...).

## 2. Clean the import

- Check rotation modes, then apply the import root's rotation and scale (FBX often brings 90° X
  and 0.01). Every object under Car Root must end at scale `(1, 1, 1)`.
- Wheel meshes often share mesh data across corners, so a normal apply fails. Rotate each shared
  mesh's data once, then zero the rotation on every object using it.
- Keep rotation where it defines how a node turns: the steering wheel (column tilt), gauge
  needles (face tilt) and mirrored right-side wheels (180°).

## 3. Wheels: Mount → Joint → Spin

- **Mount**: a new empty fixed to the body, above each Joint at the reference car's Mount→Joint
  distance (the suspension length).
- **Joint**: the wheel's kingpin node. Calipers and other non-spinning hub parts go under it.
- **Spin**: the wheel centre. The wheel and rotor meshes go under it.
- Left wheels spin on local `X`; mirrored right wheels usually on `-X`.
- Measure **radius** and **Width (m)** from the LOD0 wheel mesh's bounds; never guess.

## 4. Settings

1. **Create Configuration**, then copy the reference car's settings by script, except ids,
   object pointers, lists, presets and ghost fields.
2. Set the id, display name and class. Assign Car Root and the steering wheel with its spin axis.
3. Replace every copied value with the real car's specs:
   - engine torque curve, idle, redline, rev limit and max RPM
   - gear ratios, reverse and final drive
   - mass
   - springs, dampers, anti-roll bars
   - tyre grip and pressure
   - assists and aero (remove downforce points on cars without aero)
4. Place the **centre of mass** from the real front/rear weight split, not from the reference
   car's position. Then re-estimate brake forces with the brake estimate button.
5. Tell the user which values are still copied or estimated.

## 5. Helpers

- Make the centre of mass, cameras, collider and lamp markers with the exporter's add buttons.
- Place each one by mapping the reference car's position relative to its axles, track width and
  height onto the new car.
- Use one simple collider (`roundCuboid`) unless the shape needs more. Mass is in kg.

## 6. Materials

- Rebuild PBR from the source project's own texture files. Convert its metallic/smoothness maps to
  glTF channels: metallic from R, roughness = 1 - smoothness (often A). Pack the images into the
  `.blend`.
- No vertex colours. Bake AO and any vertex shading into one occlusion map. Check that the
  left/right UVs don't overlap first. Then delete the vertex colour layers.
- Glass: dark, with alpha.
- **Body Colors**: one paint material per colour with clear coat and the same occlusion map.
  Entry 0 is the default and must be on the body.

## 7. Hierarchy

Match the reference car: no collections; groups (empties) under Car Root:

```text
Car Root
|-- main       body, doors, Mounts, steering wheel, needles
|-- lamps      lamp meshes
|-- cameras    the 4 cameras and their targets
|-- lights     lamp markers only, never lamp meshes
|-- physics    collider, centre of mass
`-- shadows    shadow mesh, AO plane, ghost shadow, ghost shadow plane
```

The body colour carrier sits directly under Car Root, never under the collider; the preview strips
the collider. Shadow objects may sit in a group but never under a wheel or other moving node.

## 8. Lights

- Use a separate material for each function (**Headlights**, **Tail Lights**, **Brake Lights**,
  **Reverse Lights**). A shared lamp material lights every lamp at once.
- Lamp materials need Emission Strength > 0 in Blender; the game scales it.
- Tail lamps that double as brake lamps use the Tail Lights material's **Brake Lights** emission.
  Large rear lamp meshes are often the brake lamps and need the brake material to glow.
- Put lamp markers on one side only; the game mirrors them.

## 9. Gauge needles

Key each needle from zero (first frame) to maximum (last frame) and push the action to an NLA
track. Pick the actions in **Dashboard > RPM Needle / Speed Needle**. The game takes the maximums
from Max RPM and the top-gear speed.

## 10. LODs and shadows

- Name meshes `<part>_LOD<n>` under the node that moves the part. Each level shows only its own
  meshes.
- Set falling **Screen Size (%)** values and the **Ghost Default LOD**.
- After any mesh or LOD change, run all four refresh buttons: **Shadow Mesh**, **AO Plane**,
  ghost **Shadow**, ghost **Shadow Plane**.

## 11. Validate, export, test

- **Validate Car**: no errors. Read every warning; direction warnings are usually real.
- Background export to a scratch folder (only with the user's yes):

  ```text
  blender.exe --background car.NN.blend --python export.py
  ```

  `export.py`:

  ```python
  import bpy, vectorg_car_exporter as m
  s = bpy.context.scene.car_exporter
  print(m.validate_scene(s))
  for _ in m.iter_car_zip_export(bpy.context, s, r"<scratch>\<car_id>.zip", False):
      pass
  ```

- Unzip, then load the car in the game from that folder: a small local server that serves the
  unzipped package and proxies everything else to the game dev server. Open a race with the
  `browser-probes` skill's sessionStorage recipe (track, layout, vehicle, preset from
  `VectorGVehicleSetup.manifestPresets`). Ask which track to use.
- Lights test: a night practice layout through `VectorGDailyRoad.customPracticeLayoutId`. Keys:
  T lights, E high beam, V/C camera, Backspace reset.
- Installing into the user's DLC folder or `vectorg/bootstrap-dlc/vehicles/` needs a yes.

## Gotchas

- Hidden objects under Car Root without a `_LOD` suffix are dropped from the GLB, including
  group nodes; children still export. Unhide anything that must ship.
- The exported torque curve is the **Torque Curve** graph shape × Max Torque; check the numbers
  in the manifest.
- Open the exported `manifest.json` and confirm every preset value (assist levels, steering,
  camber) matches the panel.
- Stale ghost shadows: regenerate them after mesh changes.
- Copied reference values left in the manifest (power, friction, inertia, drag, steering, camber,
  sounds) must be replaced or reported.
