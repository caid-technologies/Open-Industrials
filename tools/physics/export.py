"""Build origin-locked GLB/3D Tiles visuals and individual CoACD MJCF hulls.

Run from the repository root: python -m tools.physics.export SCENE CONFIG OUTPUT.
Physics parameters are explicit input, never inferred from a visual/BOM.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import tempfile
import xml.etree.ElementTree as ET
from importlib.metadata import version
from pathlib import Path

import coacd
import mujoco
import numpy as np

from tools.geospatial.export_scene import Anchor, MAX_BYTES, digest, export_scene, finite, validate_scene, vector, write_json


def numbers(values):
    return " ".join(format(float(v), ".17g") for v in values)


def quaternion(euler):
    """Three Euler XYZ degrees -> MuJoCo wxyz; coordinates stay meters/Y-up."""
    x, y, z = (math.radians(v) / 2 for v in euler)
    c1, c2, c3, s1, s2, s3 = math.cos(x), math.cos(y), math.cos(z), math.sin(x), math.sin(y), math.sin(z)
    return [c1*c2*c3-s1*s2*s3, s1*c2*c3+c1*s2*s3, c1*s2*c3-s1*c2*s3, c1*c2*s3+s1*s2*c3]


def positive(value, label, maximum):
    value = finite(value, label)
    if not 0 < value <= maximum:
        raise ValueError(f"{label} must be positive and <= {maximum}")
    return value


def build_bundle(scene_path: Path, config_path: Path, output: Path):
    if output.exists():
        raise ValueError("Output already exists; build a new immutable bundle")
    if scene_path.stat().st_size > MAX_BYTES or config_path.stat().st_size > 256_000:
        raise ValueError("Input exceeds supported size")
    raw, config_raw = scene_path.read_bytes(), config_path.read_bytes()
    scene, config = json.loads(raw), json.loads(config_raw)
    bound, _ = validate_scene(scene)
    if set(config) != {"version", "anchor", "licenseNote", "timestep", "gravity", "coacd", "instances"} or config["version"] != 1:
        raise ValueError("Expected physics configuration version 1 with explicit parameters")
    timestep = positive(config["timestep"], "timestep", .02)
    if timestep < .0001:
        raise ValueError("timestep must be at least 0.0001 s")
    gravity = vector(config["gravity"], "gravity")
    if max(abs(v) for v in gravity) > 100:
        raise ValueError("gravity must be within +/-100 m/s^2")
    options = config["coacd"]
    if set(options) != {"threshold", "seed"} or type(options["seed"]) is not int or not 0 <= options["seed"] < 2**31:
        raise ValueError("CoACD requires a threshold and nonnegative integer seed")
    threshold = positive(options["threshold"], "CoACD normalized threshold", 1)
    visible = [i for i in scene["instances"] if i["visible"]]
    if not visible or len(visible) > 128 or set(config["instances"]) != {i["id"] for i in visible}:
        raise ValueError("Provide explicit physics for every visible instance (1..128)")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".physics-", dir=output.parent))
    try:
        export_scene(scene_path, stage, Anchor(**config["anchor"]), config["licenseNote"])
        root = ET.Element("mujoco", model="oi_physics")
        ET.SubElement(root, "compiler", angle="radian", autolimits="true", alignfree="false")
        ET.SubElement(root, "option", timestep=str(timestep), gravity=numbers(gravity), integrator="implicitfast", solver="Newton", iterations="50", tolerance="1e-10")
        assets, world, actuators = ET.SubElement(root, "asset"), ET.SubElement(root, "worldbody"), ET.SubElement(root, "actuator")
        ET.SubElement(world, "geom", name="floor", type="plane", size="0 0 .1", quat=".7071067811865476 -.7071067811865476 0 0", friction=".8 .005 .0001")
        (stage / "collision").mkdir()
        cache, records, controls = {}, [], []
        provenance = json.loads((stage / "provenance.json").read_text())
        visuals = {x["instanceId"]: x for x in provenance["instances"]}
        coacd.set_log_level("error")
        for index, instance in enumerate(visible):
            spec = config["instances"][instance["id"]]
            mode = spec.get("mode")
            common = {"mode", "friction"}
            moving = {"mass", "centerOfMass", "diagonalInertia"}
            joint = {"axis", "range", "damping", "controlRange"}
            if mode not in ("fixed", "free", "hinge", "slide") or set(spec) != common | (moving if mode != "fixed" else set()) | (joint if mode in ("hinge", "slide") else set()):
                raise ValueError(f"Invalid or incomplete physics parameters: {instance['id']}")
            friction = vector(spec["friction"], "friction")
            if any(v < 0 or v > 100 for v in friction):
                raise ValueError("friction must be within 0..100")
            name = f"body_{index}"
            body = ET.SubElement(world, "body", name=name, pos=numbers(instance["position"]), quat=numbers(quaternion(instance["rotation"])))
            if mode != "fixed":
                mass = positive(spec["mass"], "mass kg", 1e6)
                inertia = vector(spec["diagonalInertia"], "diagonal inertia kg m^2")
                if min(inertia) <= 0 or 2*max(inertia) > sum(inertia):
                    raise ValueError("Inertia must be positive and satisfy the triangle inequality")
                ET.SubElement(body, "inertial", mass=str(mass), pos=numbers(vector(spec["centerOfMass"], "center of mass")), diaginertia=numbers(inertia))
            if mode == "free":
                ET.SubElement(body, "freejoint", name=f"joint_{index}", align="false")
            if mode in ("hinge", "slide"):
                axis = vector(spec["axis"], "axis")
                if not math.isclose(sum(v*v for v in axis), 1, abs_tol=1e-6):
                    raise ValueError("Joint axis must be a unit vector in the locked body frame")
                for key in ("range", "controlRange"):
                    if not isinstance(spec[key], list) or len(spec[key]) != 2 or not -1e6 <= finite(spec[key][0], key) < finite(spec[key][1], key) <= 1e6:
                        raise ValueError(f"Invalid {key}")
                if not spec["range"][0] <= 0 <= spec["range"][1] or not spec["controlRange"][0] <= 0 <= spec["controlRange"][1]:
                    raise ValueError("Joint and control ranges must include the initial zero")
                damping = finite(spec["damping"], "damping")
                if not 0 <= damping <= 1e6:
                    raise ValueError("Invalid damping")
                ET.SubElement(body, "joint", name=f"joint_{index}", type=mode, axis=numbers(axis), range=numbers(spec["range"]), damping=str(damping))
                ET.SubElement(actuators, "motor", name=f"control_{index}", joint=f"joint_{index}", ctrllimited="true", ctrlrange=numbers(spec["controlRange"]))
                controls.append({"id": f"control_{index}", "instanceId": instance["id"], "range": spec["controlRange"], "unit": "N m" if mode == "hinge" else "N"})
            asset = bound[instance["id"]]
            key = digest(json.dumps(asset["parts"], sort_keys=True).encode())
            if key not in cache:
                hull_names = []
                for pi, part in enumerate(asset["parts"]):
                    vertices = np.asarray(part["vertices"], dtype=np.float64).reshape(-1, 3)
                    faces = np.asarray(part["indices"], dtype=np.int32).reshape(-1, 3)
                    if len(vertices) > 100_000 or len(faces) > 200_000:
                        raise ValueError("Physics decomposition limit: 100k vertices/200k triangles per part")
                    # No recentering/PCA/scale: GLB and each hull retain the same body origin.
                    hulls = coacd.run_coacd(coacd.Mesh(vertices, faces), threshold=threshold, seed=options["seed"], pca=False, max_convex_hull=64)
                    if not hulls or len(hulls) > 64:
                        raise ValueError("CoACD output exceeds the per-part hull bound")
                    for hi, (verts, tris) in enumerate(hulls):
                        mesh_name = f"h_{key[:16]}_{pi}_{hi}"
                        path = f"collision/{mesh_name}.obj"
                        lines = ["v " + numbers(v) for v in verts] + ["f " + " ".join(str(int(j)+1) for j in f) for f in tris]
                        (stage / path).write_text("\n".join(lines)+"\n")
                        ET.SubElement(assets, "mesh", name=mesh_name, file=path)
                        hull_names.append(mesh_name)
                cache[key] = hull_names
            for hi, mesh_name in enumerate(cache[key]):
                ET.SubElement(body, "geom", name=f"geom_{index}_{hi}", type="mesh", mesh=mesh_name, friction=numbers(friction), density="0")
            records.append({"instanceId": instance["id"], "name": instance["name"], "bodyName": name, "mode": mode, "visualUri": visuals[instance["id"]]["contentUri"], "hulls": cache[key], "originOffsetApplied": False})
        ET.indent(root)
        ET.ElementTree(root).write(stage / "model.xml", encoding="utf-8", xml_declaration=True)
        model = mujoco.MjModel.from_xml_path(str(stage / "model.xml"))
        data = mujoco.MjData(model)
        mujoco.mj_forward(model, data)
        for r, instance in zip(records, visible):
            r["bodyId"] = int(mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, r["bodyName"]))
            if not np.allclose(data.xpos[r["bodyId"]], instance["position"], atol=1e-10):
                raise ValueError("MuJoCo changed the visual/body origin")
        hashes = {p.relative_to(stage).as_posix(): digest(p.read_bytes()) for p in sorted(stage.rglob("*")) if p.is_file()}
        manifest = {"schema": "oi-physics-bundle/v1", "sourceSha256": digest(raw), "configSha256": digest(config_raw), "frame": "meters/Y-up/right-handed/body-origin", "anchor": config["anchor"], "timestep": timestep, "engine": {"mujoco": mujoco.__version__, "coacd": version("coacd"), "numpy": np.__version__}, "coacd": options, "instances": records, "controls": controls, "checksums": hashes, "limits": {"physics": "explicit modeled parameters, not measured hardware", "tileset": "initial static layout; live body poses are separate"}}
        write_json(stage / "manifest.json", manifest)
        stage.rename(output)
        return manifest
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scene", type=Path)
    parser.add_argument("config", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    manifest = build_bundle(args.scene, args.config, args.output)
    print(json.dumps({"bundle": str(args.output), "instances": len(manifest["instances"]), "controls": len(manifest["controls"])}))


if __name__ == "__main__":
    main()
