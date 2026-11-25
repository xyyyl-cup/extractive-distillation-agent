# -*- coding: utf-8 -*-
"""
Agent-owned NCI/RDG image renderer.

The dashboard generates nci_vmd_jobs.csv from Gaussian counterpoise dimer
jobs. This runner consumes that queue, asks Multiwfn to export NCI cube files,
renders them with VMD, and writes an importable results CSV. It is deliberately
headless and deterministic so the intelligent agent can own this evidence
stage instead of relying on manual VMD screenshots.
"""

import argparse
import csv
import datetime as dt
import json
import os
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path


FIELDNAMES = [
    "job_id",
    "component",
    "solvent_code",
    "solvent_name",
    "source_input_file",
    "source_fch",
    "work_dir",
    "func1_cube",
    "func2_cube",
    "vmd_script",
    "render_tga",
    "render_png",
    "multiwfn_log",
    "vmd_log",
    "grid_quality",
    "status",
    "error",
    "updated_at",
]


def now_iso():
    return dt.datetime.now().replace(microsecond=0).isoformat()


def safe_token(value):
    token = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value or "item")).strip("_")
    while "__" in token:
        token = token.replace("__", "_")
    return token or "item"


def resolve_executable(value):
    if not value:
        return ""
    path = Path(value)
    if path.exists():
        return str(path.resolve())
    found = shutil.which(value)
    return found or value


def read_csv_rows(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv_rows(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def as_abs(value, base):
    if not value:
        return Path()
    path = Path(value)
    if not path.is_absolute():
        path = Path(base) / path
    return path.resolve()


def png_chunk(kind, data):
    payload = kind + data
    return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)


def write_png_rgb(path, width, height, rows):
    raw = b"".join(b"\x00" + row for row in rows)
    data = b"\x89PNG\r\n\x1a\n"
    data += png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    data += png_chunk(b"IDAT", zlib.compress(raw, 9))
    data += png_chunk(b"IEND", b"")
    Path(path).write_bytes(data)


def decode_tga_to_png(tga_path, png_path):
    """Convert common VMD/Tachyon TGA output to PNG using only stdlib."""
    data = Path(tga_path).read_bytes()
    if len(data) < 18:
        raise ValueError("TGA header is too short")
    (
        id_len,
        color_map_type,
        image_type,
        _cm_first,
        _cm_len,
        _cm_depth,
        _x_origin,
        _y_origin,
        width,
        height,
        bpp,
        descriptor,
    ) = struct.unpack_from("<BBBHHBHHHHBB", data, 0)
    if color_map_type != 0:
        raise ValueError("Color-mapped TGA is not supported")
    if image_type not in {2, 10}:
        raise ValueError("Unsupported TGA image type {}".format(image_type))
    if bpp not in {24, 32}:
        raise ValueError("Unsupported TGA bpp {}".format(bpp))
    offset = 18 + id_len
    pixel_size = bpp // 8
    pixels = []
    total_pixels = int(width) * int(height)
    if image_type == 2:
        expected = total_pixels * pixel_size
        body = data[offset : offset + expected]
        if len(body) < expected:
            raise ValueError("TGA pixel data is incomplete")
        for idx in range(0, expected, pixel_size):
            b, g, r = body[idx], body[idx + 1], body[idx + 2]
            pixels.append(bytes((r, g, b)))
    else:
        idx = offset
        while len(pixels) < total_pixels and idx < len(data):
            header = data[idx]
            idx += 1
            count = (header & 0x7F) + 1
            if header & 0x80:
                if idx + pixel_size > len(data):
                    raise ValueError("TGA RLE packet is incomplete")
                b, g, r = data[idx], data[idx + 1], data[idx + 2]
                idx += pixel_size
                pixels.extend([bytes((r, g, b))] * count)
            else:
                for _ in range(count):
                    if idx + pixel_size > len(data):
                        raise ValueError("TGA raw packet is incomplete")
                    b, g, r = data[idx], data[idx + 1], data[idx + 2]
                    idx += pixel_size
                    pixels.append(bytes((r, g, b)))
    if len(pixels) < total_pixels:
        raise ValueError("TGA did not contain enough pixels")
    origin_top = bool(descriptor & 0x20)
    rows = []
    for y in range(height):
        source_y = y if origin_top else (height - 1 - y)
        start = source_y * width
        rows.append(b"".join(pixels[start : start + width]))
    write_png_rgb(png_path, width, height, rows)


def multiwfn_input(grid_quality):
    quality_code = {"low": "1", "medium": "2", "high": "3"}[grid_quality]
    return "\n".join(["20", "1", quality_code, "3", "0", "q", ""]) 


def vmd_script_text(tga_name):
    return """display projection Orthographic
display depthcue off
display rendermode GLSL
axes location Off
color Display Background white

mol new "func1.cub" type cube waitfor all
mol addfile "func2.cub" type cube waitfor all
mol delrep 0 top

mol representation CPK 1.0 0.28 18.0 16.0
mol color Name
mol addrep top

mol representation Isosurface 0.50000 1 0 0 1 1
mol color Volume 0
mol addrep top
mol scaleminmax top 1 -0.035 0.020
color scale method BGR

display resetview
rotate x by -18
rotate y by 36
rotate z by -8
scale by 1.90

render TachyonInternal "{}"
quit
""".format(tga_name.replace("\\", "/"))


def run_process(command, cwd, stdin_text=None, timeout=None):
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        input=stdin_text.encode("utf-8") if stdin_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    return completed.returncode, completed.stdout, completed.stderr


def write_log(path, stdout_bytes, stderr_bytes=None):
    text = (stdout_bytes or b"").decode("utf-8", errors="replace")
    if stderr_bytes:
        text += "\n\n[stderr]\n" + stderr_bytes.decode("utf-8", errors="replace")
    Path(path).write_text(text, encoding="utf-8", errors="replace")


def path_is_older_than(path, reference):
    return bool(path.exists() and reference.exists() and path.stat().st_mtime < reference.stat().st_mtime)


def render_one(row, args, queue_base):
    job_id = row.get("job_id") or safe_token("{}_{}".format(row.get("component"), row.get("solvent_code")))
    work_dir = as_abs(row.get("work_dir") or "", queue_base)
    if not str(work_dir):
        work_dir = Path(args.output_dir or queue_base / "nci_vmd") / safe_token(job_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    source_fch = as_abs(row.get("source_fch") or "", queue_base)
    result = {
        "job_id": job_id,
        "component": row.get("component") or "",
        "solvent_code": row.get("solvent_code") or "",
        "solvent_name": row.get("solvent_name") or "",
        "source_input_file": str(as_abs(row.get("source_input_file") or "", queue_base)) if row.get("source_input_file") else "",
        "source_fch": str(source_fch) if str(source_fch) else "",
        "work_dir": str(work_dir.resolve()),
        "func1_cube": str((work_dir / "func1.cub").resolve()),
        "func2_cube": str((work_dir / "func2.cub").resolve()),
        "vmd_script": str((work_dir / "render_nci.vmd").resolve()),
        "render_tga": str((work_dir / "nci.tga").resolve()),
        "render_png": str((work_dir / "nci.png").resolve()),
        "multiwfn_log": str((work_dir / "multiwfn_nci.log").resolve()),
        "vmd_log": str((work_dir / "vmd_render.log").resolve()),
        "grid_quality": args.grid_quality,
        "status": "",
        "error": "",
        "updated_at": now_iso(),
    }
    png_path = Path(result["render_png"])
    tga_path = Path(result["render_tga"])
    work_fch = work_dir / "input.fch"
    func1_path = work_dir / "func1.cub"
    func2_path = work_dir / "func2.cub"
    input_path = Path(result["source_input_file"]) if result.get("source_input_file") else None
    stale_fch = bool(input_path and input_path.exists() and source_fch.exists() and source_fch.stat().st_mtime < input_path.stat().st_mtime)
    stale_png = bool(input_path and input_path.exists() and png_path.exists() and png_path.stat().st_mtime < input_path.stat().st_mtime)
    stale_work_products = bool(
        source_fch.exists()
        and any(path_is_older_than(path, source_fch) for path in [work_fch, func1_path, func2_path, tga_path, png_path])
    )
    if png_path.exists() and png_path.stat().st_size > 0 and not args.force:
        if stale_fch:
            result["status"] = "blocked_stale_fch"
            result["error"] = "Gaussian input is newer than fch; rerun Gaussian optimization before NCI rendering"
            return result
        if not stale_png and not stale_work_products:
            result["status"] = "rendered_png"
            return result
    if stale_png or stale_work_products or args.force:
        for stale_path in [work_fch, func1_path, func2_path, tga_path, png_path]:
            if stale_path.exists():
                stale_path.unlink()
        result["error"] = "Regenerated stale NCI work products from current source_fch"
    if png_path.exists() and png_path.stat().st_size > 0 and not args.force:
        result["status"] = "rendered_png"
        return result
    if not source_fch.exists():
        result["status"] = "blocked_missing_fch"
        result["error"] = "source_fch does not exist"
        return result
    if stale_fch:
        result["status"] = "blocked_stale_fch"
        result["error"] = "source_fch is older than source_input_file; rerun Gaussian optimization"
        return result
    if source_fch.resolve() != work_fch.resolve():
        shutil.copyfile(source_fch, work_fch)
    multiwfn = resolve_executable(args.multiwfn)
    if not multiwfn or not Path(multiwfn).exists():
        result["status"] = "blocked_missing_multiwfn"
        result["error"] = "Multiwfn executable not found"
        return result
    if not func1_path.exists() or not func2_path.exists():
        try:
            rc, out, err = run_process(
                [multiwfn, "input.fch"],
                work_dir,
                stdin_text=multiwfn_input(args.grid_quality),
                timeout=args.multiwfn_timeout_seconds,
            )
            write_log(result["multiwfn_log"], out, err)
            if rc != 0:
                result["error"] = "Multiwfn returned {}".format(rc)
        except Exception as exc:
            result["status"] = "failed_multiwfn"
            result["error"] = str(exc)
            return result
    if not func1_path.exists() or not func2_path.exists():
        result["status"] = "failed_multiwfn_no_cubes"
        if not result["error"]:
            result["error"] = "func1.cub/func2.cub were not generated"
        return result
    vmd = resolve_executable(args.vmd)
    if not vmd or not Path(vmd).exists():
        result["status"] = "cubes_generated_vmd_missing"
        result["error"] = "VMD executable not found"
        return result
    Path(result["vmd_script"]).write_text(vmd_script_text("nci.tga"), encoding="utf-8")
    try:
        rc, out, err = run_process(
            [vmd, "-dispdev", "text", "-e", "render_nci.vmd"],
            work_dir,
            timeout=args.vmd_timeout_seconds,
        )
        write_log(result["vmd_log"], out, err)
        if rc != 0 and not tga_path.exists():
            result["status"] = "failed_vmd"
            result["error"] = "VMD returned {}".format(rc)
            return result
    except Exception as exc:
        result["status"] = "failed_vmd"
        result["error"] = str(exc)
        return result
    if not tga_path.exists() or tga_path.stat().st_size <= 0:
        result["status"] = "failed_vmd_no_render"
        result["error"] = "VMD did not create nci.tga"
        return result
    try:
        decode_tga_to_png(tga_path, png_path)
    except Exception as exc:
        result["status"] = "rendered_tga"
        result["error"] = "PNG conversion failed: {}".format(exc)
        return result
    result["status"] = "rendered_png" if png_path.exists() and png_path.stat().st_size > 0 else "rendered_tga"
    if result["status"] == "rendered_png" and str(result.get("error") or "").startswith("Multiwfn returned"):
        result["error"] = ""
    return result


def build_self_test_row(args):
    fch = Path(args.self_test_fch).resolve()
    job_id = "selftest_{}".format(safe_token(args.self_test_label or fch.stem))
    root = Path(args.output_dir or Path.cwd() / "nci_vmd_selftest").resolve()
    return {
        "job_id": job_id,
        "component": args.self_test_label or fch.stem,
        "solvent_code": args.self_test_solvent or "SELFTEST",
        "solvent_name": args.self_test_solvent or "self test",
        "source_fch": str(fch),
        "source_input_file": "",
        "work_dir": str((root / safe_token(job_id)).resolve()),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run agent NCI/VMD image jobs.")
    parser.add_argument("--queue", default="", help="nci_vmd_jobs.csv")
    parser.add_argument("--results", default="", help="nci_vmd_results.csv")
    parser.add_argument("--output-dir", default="", help="root output directory")
    parser.add_argument("--multiwfn", default=os.environ.get("MULTIWFN_EXE", r"D:\GROMACS\sobtop_2026.1.16\Multiwfn.exe"))
    parser.add_argument("--vmd", default=os.environ.get("VMD_EXE", r"D:\Program Files (x86)\University of Illinois\VMD\vmd.exe"))
    parser.add_argument("--grid-quality", choices=["low", "medium", "high"], default="low")
    parser.add_argument("--max-jobs", type=int, default=0)
    parser.add_argument("--solvent-code", default="")
    parser.add_argument("--multiwfn-timeout-seconds", type=int, default=1800)
    parser.add_argument("--vmd-timeout-seconds", type=int, default=300)
    parser.add_argument("--force", action="store_true", help="rerender even if nci.png already exists")
    parser.add_argument("--self-test-fch", default="")
    parser.add_argument("--self-test-label", default="")
    parser.add_argument("--self-test-solvent", default="")
    args = parser.parse_args(argv)

    if args.self_test_fch:
        rows = [build_self_test_row(args)]
        queue_base = Path(args.output_dir or Path.cwd()).resolve()
    else:
        if not args.queue:
            parser.error("--queue is required unless --self-test-fch is used")
        queue = Path(args.queue).resolve()
        rows = read_csv_rows(queue)
        queue_base = queue.parent

    selected = []
    solvent_filter = (args.solvent_code or "").strip().upper()
    for row in rows:
        if solvent_filter:
            tokens = {
                str(row.get("solvent_code") or "").strip().upper(),
                str(row.get("solvent_name") or "").strip().upper(),
            }
            if solvent_filter not in tokens:
                continue
        selected.append(row)
    if args.max_jobs and args.max_jobs > 0:
        selected = selected[: args.max_jobs]

    results = [render_one(row, args, queue_base) for row in selected]
    result_path = Path(args.results).resolve() if args.results else (queue_base / "nci_vmd_results.csv")
    write_csv_rows(result_path, results)
    rendered = [
        row for row in results
        if row.get("status") in {"rendered_png", "rendered_tga"} and Path(row.get("render_tga") or "").exists()
    ]
    png_rendered = [
        row for row in results
        if row.get("status") == "rendered_png" and Path(row.get("render_png") or "").exists()
    ]
    summary = {
        "status": "rendered" if rendered else "no_rendered_images",
        "selected_job_count": len(selected),
        "result_count": len(results),
        "rendered_count": len(rendered),
        "rendered_png_count": len(png_rendered),
        "results_csv": str(result_path),
        "updated_at": now_iso(),
    }
    write_json(result_path.with_suffix(".summary.json"), summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if rendered else 2


if __name__ == "__main__":
    raise SystemExit(main())
