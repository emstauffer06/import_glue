"""Bounded, self-contained static IES and OSL capture for private node copies.

No source shader or sibling .oso is ever written. OSL runtime file operations
remain an explicit exclusion: a filename may be computed inside the program.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import tempfile
import uuid

import bpy

MAX_RESOURCE_BYTES = 16 * 1024 * 1024
MAX_INCLUDE_FILES = 128
MAX_INCLUDE_DEPTH = 24
_FILE_OPS = frozenset({"texture", "texture3d", "environment", "gettextureinfo",
                       "pointcloud_search", "pointcloud_get", "pointcloud_write", "dict_find"})
_CONTEXT_OPS = frozenset({"getattribute", "trace", "getmessage", "setmessage", "getmatrix",
                         "transform", "transformv", "transformn"})


class ResourceError(RuntimeError):
    pass


def _validate_bytecode(bytecode):
    if not bytecode.startswith("OpenShadingLanguage "):
        raise ResourceError("OSL compiled bytecode is unavailable; update the script node first")
    for line in bytecode.splitlines():
        tokens = line.split()
        if tokens and tokens[0] in _FILE_OPS:
            raise ResourceError("OSL runtime file operation is not captured: " + tokens[0])
        if tokens and tokens[0] in _CONTEXT_OPS:
            raise ResourceError("OSL scene or identity context cannot be proved on a copied object: " + tokens[0])


def validate_internal_node(node):
    """Guard already-internal bytecode as strictly as external OSL capture."""
    if node.bl_idname != "ShaderNodeScript" or node.mode != "INTERNAL":
        raise ResourceError("Expected an internal OSL node")
    if node.script is None or node.use_auto_update:
        raise ResourceError("OSL requires a compiled internal text with automatic recompilation disabled")
    if bpy.context.scene.render.engine != "CYCLES" or not bpy.context.scene.cycles.shading_system:
        raise ResourceError("OSL requires the source scene's Cycles OSL shading system")
    _validate_bytecode(node.bytecode)
    return True


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _read(path, inventory):
    path = Path(path).resolve()
    if not path.is_file() or path.stat().st_size > MAX_RESOURCE_BYTES:
        raise ResourceError("Shader resource missing or exceeds byte limit: " + str(path))
    data = path.read_bytes()
    inventory[str(path)] = {"path": str(path), "sha256": _hash(data), "bytes": len(data)}
    if len(inventory) > MAX_INCLUDE_FILES or sum(row["bytes"] for row in inventory.values()) > MAX_RESOURCE_BYTES:
        raise ResourceError("Shader resource dependency budget exceeded")
    return data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")


def _stdlib():
    import cycles
    return Path(cycles.__file__).resolve().parent / "shader"


def _flatten(path, inventory, ancestors=()):
    path = Path(path).resolve()
    if path in ancestors or len(ancestors) >= MAX_INCLUDE_DEPTH:
        raise ResourceError("Recursive or over-depth OSL include: " + str(path))
    source = _read(path, inventory)
    # Accept only literal include directives. Conditional directives are kept;
    # even inactive literal includes are captured conservatively.
    def include(match):
        literal = re.fullmatch(r'\s*["<]([^">]+)[">]\s*(?://[^\n]*)?', match.group(1))
        if literal is None:
            raise ResourceError("Computed OSL include path cannot be captured")
        name = literal.group(1)
        candidates = [path.parent / name, _stdlib() / name]
        target = next((candidate for candidate in candidates if candidate.is_file()), None)
        if target is None:
            raise ResourceError("OSL include is unavailable: " + name)
        return "\n" + _flatten(target, inventory, ancestors + (path,)) + "\n"
    return re.sub(r'^\s*#\s*include\b([^\n]*)', include, source, flags=re.M)


def _osl(node, path, inventory):
    import _cycles
    if not getattr(bpy.context.scene.cycles, "shading_system", False):
        raise ResourceError("OSL capture requires the source scene's Cycles OSL shading system")
    if bpy.context.scene.render.engine != "CYCLES":
        raise ResourceError("OSL capture requires the Cycles renderer")
    if path.suffix.lower() != ".osl":
        raise ResourceError("OSL capture requires .osl source to prove the include and runtime resource closure")
    flattened = _flatten(path, inventory)
    # The compiler injects its standard header even without an explicit include.
    _flatten(_stdlib() / "stdosl.h", inventory)
    with tempfile.TemporaryDirectory(prefix="import-glue-osl-") as temporary:
        output = Path(temporary) / "capture.oso"
        if not _cycles.osl_compile(str(path), str(output)) or not output.is_file():
            raise ResourceError("OSL compilation failed for the captured renderer")
        bytecode = output.read_text(encoding="utf-8")
        # Inspect compiled instructions as well as source: macros and includes
        # cannot hide texture/point-cloud operations from this check.
        _validate_bytecode(bytecode)
        sibling = path.with_suffix(".oso")
        def instructions(text):
            # oslc embeds the output temporary filename in this comment. Every
            # executable instruction, constant, include location and compiler
            # version must still match the source's existing compiled shader.
            return "\n".join(line for line in text.splitlines() if not line.startswith("# options:"))
        if not sibling.is_file() or instructions(sibling.read_text(encoding="utf-8")) != instructions(bytecode):
            raise ResourceError("OSL source and its current compiled .oso differ or compilation is missing; update the source script node first")
        _read(sibling, inventory)
        # Socket reconciliation would modify source semantics. Require the
        # already configured node to match the compiled interface instead.
        import oslquery
        query = oslquery.OSLQuery(str(output))
        if not query:
            raise ResourceError("OSL compiled interface could not be inspected")
        from cycles.osl import shader_param_type_default
        expected = []
        for parameter in query.parameters:
            if parameter.varlenarray or parameter.isstruct or parameter.type.arraylen > 1:
                continue
            metadata = {meta.name: meta.value for meta in parameter.metadata}
            socket_type, _default = shader_param_type_default(parameter, metadata.get("widget") in {"boolean", "checkBox"})
            if socket_type:
                expected.append((bool(parameter.isoutput), parameter.name, socket_type))
        actual = [(output, socket.identifier, socket.bl_idname)
                  for output, sockets in ((False, node.inputs), (True, node.outputs)) for socket in sockets]
        if actual != sorted(expected, key=lambda row: row[0]):
            raise ResourceError("OSL node sockets differ from the current compiled source; update the source script node first")
    return {"kind": "OSL", "text": flattened, "bytecode": bytecode,
            "compiler": {"blender_version": list(bpy.app.version),
                         "build_hash": bpy.app.build_hash.decode("utf-8", "replace"),
                         "shading_system": True}, "runtime_files": "excluded"}


def _ies(path, inventory):
    text = _read(path, inventory)
    match = re.search(r'^\s*TILT\s*=\s*(.*?)\s*$', text, re.M | re.I)
    if match is None:
        raise ResourceError("IES resource lacks a TILT declaration")
    tilt = match.group(1)
    if tilt.upper() not in {"NONE", "INCLUDE"}:
        tilt_data = _read(path.parent / tilt, inventory)
        text = text[:match.start()] + "TILT=INCLUDE\n" + tilt_data + "\n" + text[match.end():]
    return {"kind": "IES", "text": text}


def prepare_node(node):
    if node.bl_idname not in {"ShaderNodeScript", "ShaderNodeTexIES"} or node.mode != "EXTERNAL":
        raise ResourceError("Expected an external OSL or IES node")
    if node.id_data.library is not None:
        raise ResourceError("Linked external shader nodes require a local material copy")
    path = Path(bpy.path.abspath(node.filepath, library=node.id_data.library)).resolve()
    inventory = {}
    result = _osl(node, path, inventory) if node.bl_idname == "ShaderNodeScript" else _ies(path, inventory)
    result.update(source_path=str(path), dependencies=sorted(inventory.values(), key=lambda row: row["path"]))
    result["text_sha256"] = _hash(result["text"].encode("utf-8"))
    if "bytecode" in result:
        result["bytecode_sha256"] = _hash(result["bytecode"].encode("utf-8"))
    return result


def inspect_node(node):
    try:
        prepared = prepare_node(node)
        return {"supported": True, "reason": "Verified static %s resource can be embedded" % prepared["kind"],
                "kind": prepared["kind"], "dependency_count": len(prepared["dependencies"])}
    except Exception as exc:
        return {"supported": False, "reason": str(exc)}


def capture_node(node, texts, *, prepared=None):
    """Embed on a private node; caller owns/removes appended Text datablocks."""
    result = prepared if prepared is not None else prepare_node(node)
    text = bpy.data.texts.new("__IG_RESOURCE_" + uuid.uuid4().hex)
    texts.append(text)
    text.write(result["text"])
    text.use_fake_user = False
    node.mode = "INTERNAL"
    if result["kind"] == "IES":
        node.ies = text
    else:
        node.script = text
        node.use_auto_update = False
        node.bytecode = result["bytecode"]
        node.bytecode_hash = hashlib.md5(result["bytecode"].encode("utf-8"), usedforsecurity=False).hexdigest()
    return {key: value for key, value in result.items() if key not in {"text", "bytecode"}}


def verify_node(node, record):
    if node.mode != "INTERNAL":
        raise ResourceError("Captured shader resource is no longer internal")
    text = node.ies if record["kind"] == "IES" else node.script
    if text is None or _hash(text.as_string().encode("utf-8")) != record["text_sha256"]:
        raise ResourceError("Captured shader text changed")
    if record["kind"] == "OSL":
        if _hash(node.bytecode.encode("utf-8")) != record["bytecode_sha256"]:
            raise ResourceError("Captured OSL bytecode changed")
        context = record["compiler"]
        if (list(bpy.app.version) != context["blender_version"] or
                bpy.app.build_hash.decode("utf-8", "replace") != context["build_hash"] or
                not bpy.context.scene.cycles.shading_system):
            raise ResourceError("Captured OSL compiler/shading context changed")
    return True
