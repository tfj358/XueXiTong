"""不可覆盖的发布流水线；仅收集白名单源码，不收集运行账号及个人日志。"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import zipfile


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def unique_dir(parent, name):
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    while True:
        target = parent / name
        try:
            target.mkdir()
            return target
        except FileExistsError:
            name = f"{name}-{uuid.uuid4().hex[:8]}"


def version_resource(version, filename):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("version.txt must contain major.minor.patch")
    parts = tuple(map(int, version.split("."))) + (0,)
    if any(n > 65535 for n in parts):
        raise ValueError("Windows version component exceeds 65535")
    strings = {"CompanyName": "MicroYume", "FileDescription": "学习通助手桌面版",
               "FileVersion": version + ".0", "InternalName": "学习通助手",
               "OriginalFilename": filename, "ProductName": "学习通助手", "ProductVersion": version}
    entries = ",\n".join(f"StringStruct({k!r}, {v!r})" for k, v in strings.items())
    return (f"VSVersionInfo(ffi=FixedFileInfo(filevers={parts!r}, prodvers={parts!r}, "
            "mask=0x3f, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0,0)), "
            f"kids=[StringFileInfo([StringTable('080404b0', [{entries}])]), "
            "VarFileInfo([VarStruct('Translation', [2052,1200])])])\n")


def source_snapshot(source, upstream, destination):
    source, upstream = Path(source), Path(upstream)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.iterdir()):
            if path.is_file() and (path.suffix in {".py", ".ps1", ".spec", ".ico", ".md"} or path.name == "version.txt"):
                archive.write(path, "desktop-source/" + path.name)
        for name in ("LICENSE", "README.md", "THIRD_PARTY_NOTICES.md", "pyproject.toml"):
            archive.write(upstream / name, "superstar-checker/" + name)
        for folder in ("src", "tests"):
            for path in sorted((upstream / folder).rglob("*")):
                allowed = path.suffix == ".py" or path.relative_to(upstream).as_posix() == "src/superstar_checker/data/font_map_table.json"
                if path.is_file() and allowed and "__pycache__" not in path.parts:
                    archive.write(path, "superstar-checker/" + path.relative_to(upstream).as_posix())


def manifest(directory):
    directory = Path(directory)
    return {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest().upper()
            for p in sorted(directory.rglob("*")) if p.is_file() and p.name != "SHA256.json"}


def history_hashes(release_base):
    release_base = Path(release_base)
    return {p.relative_to(release_base).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest().upper()
            for p in sorted(release_base.rglob("*"))
            if p.is_file() and not p.relative_to(release_base).parts[0].startswith(".")}


def publish(stage, release_root):
    """单次同卷重命名发布，目标存在时另选唯一名称。"""
    stage, release_root = Path(stage), Path(release_root)
    release_root.mkdir(parents=True, exist_ok=True)
    target = release_root / stage.name
    while target.exists():
        target = release_root / f"{stage.name}-{uuid.uuid4().hex[:8]}"
    stage.rename(target)
    return target


def run_logged(command, log, *, cwd, env=None, timeout=600):
    with Path(log).open("w", encoding="utf-8") as stream:
        result = subprocess.run(command, cwd=cwd, env=env, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=timeout,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}), see {Path(log).name}")


def build(source, release_dir=None, output_name=None, *, runner=run_logged):
    source = Path(source).resolve()
    outputs = source.parent
    upstream = outputs / "superstar-checker"
    version = (source / "version.txt").read_text(encoding="utf-8").strip()
    name = output_name or f"学习通助手-增强版-v{version}.exe"
    if Path(name).name != name or not name.lower().endswith(".exe"):
        raise ValueError("OutputName must be an EXE filename")
    resource = version_resource(version, name)
    release_root = Path(release_dir).resolve() if release_dir else outputs / "release" / version
    historical = history_hashes(release_root.parent)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    stage = unique_dir(release_root.parent / ".staging", stamp)
    work = unique_dir(outputs.parent / "work", "build-" + stamp)
    version_path = work / "version_info.txt"
    version_path.write_text(resource, encoding="utf-8")
    env = dict(os.environ, SUPERSTAR_VERSION_FILE=str(version_path), PYTHONIOENCODING="utf-8",
               SUPERSTAR_DATA_DIR=str(work / "isolated-account"))
    write_json(stage / "build-status.json", {"version": version, "status": "building", "work": str(work)})
    try:
        runner([sys.executable, "-m", "unittest", "discover", "-s", str(source), "-p", "test_*.py", "-v"],
               stage / "tests.log", cwd=source, env=env)
        runner([sys.executable, str(upstream / "tests" / "unit_test.py")],
               stage / "upstream-tests.log", cwd=upstream, env=env)
        runner([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(work / "dist"),
                "--workpath", str(work / "pyinstaller"), str(source / "desktop.spec")],
               stage / "build.log", cwd=source, env=env)
        target = stage / name
        shutil.copy2(work / "dist" / "学习通助手.exe", target)
        for flag, report in (("--check-install", "check-install.json"), ("--smoke-test", "smoke-test.json")):
            runner([str(target), flag, str(stage / report)], stage / (report + ".log"),
                   cwd=stage, env=env, timeout=45)
        installation = json.loads((stage / "check-install.json").read_text(encoding="utf-8"))
        if not all(installation.get(k) for k in ("frozen", "offline", "aes_ok", "des_ok", "notifications_ok", "font_table_entries")) or installation["app_version"] != version:
            raise RuntimeError("Frozen installation report failed")
        smoke = json.loads((stage / "smoke-test.json").read_text(encoding="utf-8"))
        if not all(smoke.get(k) for k in ("frozen", "mock_backend", "window_visible", "closed")) or smoke.get("errors") or smoke.get("course_count") != 1:
            raise RuntimeError("Frozen window smoke report failed")
        import win32api
        pe = win32api.GetFileVersionInfo(str(target), "\\")
        actual = (pe["FileVersionMS"] >> 16, pe["FileVersionMS"] & 65535,
                  pe["FileVersionLS"] >> 16, pe["FileVersionLS"] & 65535)
        if actual != tuple(map(int, version.split("."))) + (0,):
            raise RuntimeError(f"PE version mismatch: {actual}")
        write_json(stage / "version-check.json", {"expected": version + ".0", "actual": ".".join(map(str, actual)), "passed": True})
        shutil.copy2(version_path, stage / "version_info.txt")
        shutil.copy2(outputs / "桌面版使用说明.md", stage / "使用说明.md")
        shutil.copy2(source / "CHANGELOG.md", stage / "CHANGELOG.md")
        shutil.copytree(outputs / "第三方声明", stage / "第三方声明")
        source_snapshot(source, upstream, stage / "source.zip")
        runner([sys.executable, "-m", "pip", "freeze"], stage / "dependencies.txt", cwd=source, env=env)
        current_history = history_hashes(release_root.parent)
        if any(current_history.get(path) != digest for path, digest in historical.items()):
            raise RuntimeError("Previously published artifacts changed during build")
        write_json(stage / "history-check.json", {"passed": True, "files_checked": len(historical),
                   "sha256_before": historical})
        write_json(stage / "build-status.json", {"version": version, "status": "passed",
                   "built_at": datetime.now(timezone.utc).isoformat(), "executable": name})
        write_json(stage / "SHA256.json", manifest(stage))
        if manifest(stage) != json.loads((stage / "SHA256.json").read_text(encoding="utf-8")):
            raise RuntimeError("Artifact hash verification failed")
        final = publish(stage, release_root)
        print(str(final), flush=True)
        return final
    except BaseException as exc:
        write_json(stage / "build-status.json", {"version": version, "status": "failed", "error": str(exc), "work": str(work)})
        failure = publish(stage, release_root.parent / ".failed" / version)
        print(f"Failed build retained: {failure}", file=sys.stderr, flush=True)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["build"])
    parser.add_argument("--release-dir")
    parser.add_argument("--output-name")
    args = parser.parse_args()
    build(Path(__file__).parent, args.release_dir, args.output_name)
