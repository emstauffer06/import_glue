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

## New in 1.6.0

- **Delivery targets.** Blender Native and Roblox outputs can be produced separately or together. The four-map workflow from 1.4.0 remains as **Legacy Four Maps**.
- **Roblox emission.** Baked emission is converted to an emissive mask, tint and strength, measured against the delivered colour map, and refused when the error is too large unless you allow approximation.
- **Background jobs.** Bakes run in a separate Blender process, with cancel, resume from verified checkpoints, and an explicit step to append finished results.
- **Source snapshots.** Unsaved texture painting is captured, and your original file is left unchanged.
- **Resource checks and UV diagnostics.** Free RAM and NVIDIA VRAM are measured before a job, one GPU can be chosen explicitly, and UV density, distortion and clearance are reported.
- **PNG16.** The four legacy maps can be baked as true 16-bit PNG.

> **Test status:** 1.6.0 was validated in headless Blender 5.2.2 on Linux. The Blender GUI on Windows and import into Roblox Studio have not been tested with this build yet. The previous release, [1.4.0](https://github.com/emstauffer06/import_glue/releases/tag/v1.4.0-engine3.8), remains available.

> **Changed from 1.4.0:** the panel now defaults to **Blender + Roblox**. Choose **Legacy Four Maps** for the 1.4.0 behaviour. Materials the maps cannot carry are still refused by default; tick **Allow Approximated Materials** under Advanced to bake them, labelled `APPROXIMATED`.

## Download and install

1. Download the zip from the [latest release](https://github.com/emstauffer06/import_glue/releases/latest). Do not unzip it.
2. In Blender, open **Edit > Preferences > Add-ons**, choose **Install from Disk**, and select the zip.
3. Enable **Import Glue PBR Baker**.
4. In the 3D Viewport press **N**, open the **Roblox** tab, and use **Import Glue**.

Requires Blender 4.3 or newer. Restart Blender after replacing an older version of the add-on.

## Documentation

- **[Release notes for 1.6.0](docs/RELEASE_NOTES_1.6.0.md):** the new targets, background jobs, validation results and known limits.
- **[Quick guide (PDF)](docs/import_glue_quick_guide.pdf):** a two-page visual walkthrough of the four-map workflow, written for 1.4.0.
- **[User guide](docs/USER_GUIDE.md):** the full manual for the four-map workflow, including the capability report, checkpoints and the visual check. Written for 1.4.0.
- **[Release notes for 1.4.0](docs/RELEASE_NOTES_1.4.0.md):** what changed between 1.3.0 and 1.4.0.
- **[Add-on README](import_glue_pbr_baker/README.md):** resolution settings, safety behavior, headless flags, and known engine limits.

## License

[GPL-3.0-or-later](LICENSE)
