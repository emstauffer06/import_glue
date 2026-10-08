![Import Glue PBR Baker: bake game-rip materials into Roblox-ready PBR maps](docs/banner.png)

# Import Glue PBR Baker

A Blender add-on that converts game-rip meshes into Roblox-ready meshes and PBR texture maps, and can also bake a Blender-native version that keeps the original shader structure.

Select the meshes you want, choose a **Delivery Target**, and start the bake:

| Target | What you get |
| --- | --- |
| **Roblox** | Five PNG maps per part (colour, normal, metalness, roughness, emissive mask), a triangulated FBX with tangents, a manifest and a generated Luau importer |
| **Blender Native** | The original shader structure, with eligible inputs baked to float32 EXR, saved as an appendable `.blend` |
| **Blender + Roblox** | Both of the above (the default) |
| **Legacy Four Maps** | The classic `_ALB`, `_MET`, `_RGH`, `_NOR` PNG set, with checkpoints |

Your source objects, meshes, UVs, and materials are never edited. Each job works from a private snapshot and writes its results, a manifest and a run log to a new output folder.

## What it does

- **Picks a route per part (Legacy Four Maps).** A part that already maps to one flat atlas with compact UVs is cropped straight from its textures. A part that needs repacking is baked through a proxy. Anything else, including complex shader graphs and meshes with no usable UVs, is baked with Cycles.
- **Checks textures first.** Missing or corrupt material textures are reported before any baking starts.
- **Respects the Roblox triangle limit.** Parts over the configured limit are decimated on the graph-bake route and rejected on the others, rather than exported silently.
- **Resumes safely.** Parts whose inputs and outputs are unchanged since the last run are carried over; anything that changed is baked again.
- **Runs headless.** A background launcher supports batch runs from the command line, with exit codes for scripting.

## New in 1.8.0

- **Fit Roblox Material** (optional). Tunes each object's roughness and metalness maps by rendering them against the original material, checks the result on separate camera and light angles, and keeps the normal result if nothing helps.
- **Capture Static Displacement** (optional). Turns a supported kind of displacement into real mesh geometry before the FBX export, within a triangle budget.
- **Large source images.** 8K images can be verified and kept at full size in Blender Native output. Roblox maps stay capped at 4096.
- **Preview Targets and Cost.** Shows the planned work, what will be refused and the estimated memory before you bake.
- **Better Blender Native bakes** (from 1.7). Adaptive resolution with measured error, seam padding within UV islands, visual comparison galleries, and checkpoints so interrupted jobs resume without rebaking.
- **More sources handled** (from 1.7). Dirty UDIM tiles, image sequences, OSL and IES files, custom colour configurations and Generated coordinates on deformed meshes.

The fitting and displacement options are off by default and need **Allow Approximated Materials** turned on.

> **Requires Blender 5.2 or newer.** On an older Blender, use [1.6.0](https://github.com/emstauffer06/import_glue/releases/tag/v1.6.0-engine4.0), which works from Blender 4.3.

> **Test status:** the package was installed and checked in Blender 5.2.2 on Windows and Linux, and the full test suite ran on Linux. Import into Roblox Studio has not been tested.

## Download and install

1. Download the zip from the [latest release](https://github.com/emstauffer06/import_glue/releases/latest). Do not unzip it.
2. In Blender, open **Edit > Preferences > Add-ons**, choose **Install from Disk**, and select the zip.
3. Enable **Import Glue PBR Baker**.
4. In the 3D Viewport press **N**, open the **Roblox** tab, and use **Import Glue**.

Requires Blender 5.2 or newer. Restart Blender after replacing an older version of the add-on.

## Documentation

- **[Release notes for 1.8.0](docs/RELEASE_NOTES_1.8.0.md):** material fitting, displacement capture, large images, the 1.7 native-quality work, validation results and known limits.
- **[Release notes for 1.6.0](docs/RELEASE_NOTES_1.6.0.md):** the delivery targets and background jobs.
- **[Quick guide (PDF)](docs/import_glue_quick_guide.pdf):** a two-page visual walkthrough: installing, baking in the background, the delivery targets, settings and common fixes.
- **[User guide](docs/USER_GUIDE.md):** the full manual for the four-map workflow, including the capability report, checkpoints and the visual check. Written for 1.4.0.
- **[Release notes for 1.4.0](docs/RELEASE_NOTES_1.4.0.md):** what changed between 1.3.0 and 1.4.0.
- **[Add-on README](import_glue_pbr_baker/README.md):** resolution settings, safety behavior, headless flags, and known engine limits.

## License

[GPL-3.0-or-later](LICENSE)
