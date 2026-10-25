#!/usr/bin/env python3
"""Validate version/tag and built archives without importing runtime integrations."""
import argparse
import ast
from email.parser import Parser
from pathlib import Path
import re
import tarfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def version():
    tree = ast.parse((ROOT / "src/noc_ops/__init__.py").read_text())
    return next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "__version__" for t in n.targets))


def check(tag=None, dist=None):
    release = version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", release), "需要稳定版本 X.Y.Z"
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["dynamic"] == ["version"]
    assert project["tool"]["hatch"]["version"]["path"] == "src/noc_ops/__init__.py"
    assert f"## {release}" in (ROOT / "CHANGELOG.md").read_text(), "缺少版本更新记录"
    if tag:
        assert tag == "v" + release, "发布 tag 必须与代码版本一致"
    if dist:
        files = list(Path(dist).glob("*.whl")) + list(Path(dist).glob("*.tar.gz"))
        assert len(files) == 2, "发布目录必须只有当前版本的 wheel 与 sdist"
        for file in files:
            if file.suffix == ".whl":
                with zipfile.ZipFile(file) as archive:
                    names = archive.namelist()
                    metadata = archive.read(next(n for n in names if n.endswith('.dist-info/METADATA'))).decode()
                    required = ("noc_ops/dashboard/index.html", "noc_ops/dashboard/vendor/ag-grid-community.min.js",
                                "noc_ops/skill/SKILL.md.tmpl", "noc_ops/assets/ops-config.default.toml")
                    assert all(n in names for n in required), "wheel 缺少运行资产"
            else:
                with tarfile.open(file) as archive:
                    names = archive.getnames()
                    metadata = archive.extractfile(next(n for n in names if n.endswith('/PKG-INFO'))).read().decode()
            assert Parser().parsestr(metadata)["Version"] == release, "产物版本不一致"
            assert not any(n.endswith('/data.json') or '/.state/' in n or n.endswith('/ops-config.toml')
                           or '/noc_internal/' in n or '/noc_ops_internal/' in n for n in names), "发行包混入工作区或内部扩展"
    print(f"release {release}: OK")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag")
    parser.add_argument("--dist")
    args = parser.parse_args()
    check(args.tag, args.dist)
