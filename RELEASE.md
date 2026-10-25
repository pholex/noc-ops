# 发布与更新

版本唯一来源是 `src/noc_ops/__init__.py` 的 `__version__`，Hatch 从这里生成包元数据；客户端 skill 由 `ops setup` 注入同一版本。Python 最低版本为 3.11。

## 用户更新

```sh
ops update --check       # 查询 PyPI 稳定版与本机、skill 版本，不安装
ops update --dry-run     # 本机预览，不联网、不安装
ops update              # 展示步骤，确认后更新
ops update --yes        # 已确认的自动化调用
```

0.2.x 尚无 `ops update` 时，先运行 `uv tool upgrade noc-ops`，再运行 `ops setup`。

更新使用 `uv tool upgrade noc-ops`，保留 uv 记录的安装来源、extra、扩展和版本约束；随后使用更新后的 `ops setup` 刷新 skill。工作区数据不参与更新。固定版本或本地源码安装不会自动切换到 PyPI 最新版；若要切换来源或回退版本，请用最初的 `uv tool install` 参数（包括所有 `--with` 扩展）明确安装目标版本，再运行 `ops setup`。pip、源码虚拟环境等非 uv tool 安装只支持版本查询，请使用原安装方式更新。

## 维护者发布

1. 修改 `__version__` 并更新 `CHANGELOG.md`；同步 `multicloud-ops` 引导 skill 的版本及能力说明。
2. 跑 `python -m pytest -m unit -q`、`python -m build`、`python scripts/check-release.py --dist dist`、`python -m twine check dist/*`。构建目录只保留这次 wheel/sdist。
3. 审查后提交代码，给同一提交打 `vX.Y.Z` tag。确认发布后推送分支与 tag。
4. `release.yml` 先执行跨系统测试、构建与安装检查，再校验 tag/产物版本，最后通过 PyPI Trusted Publishing 发布。普通分支 push 不发布包。

首次发布前，维护者需在 PyPI 为 `pholex/noc-ops`、工作流 `release.yml`、GitHub environment `pypi` 配置 Trusted Publisher；GitHub 侧设置对应 environment。仓库中不存发布 token。此配置需要仓库/PyPI 所有者权限，本地新增工作流不会自动建立远端授权。

参考：[uv 工具更新](https://docs.astral.sh/uv/guides/tools/#upgrading-tools)、[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/)。

已通过本机凭据发布的版本，工作流会校验 PyPI 文件名与 SHA-256 完全一致后跳过重复上传。新版本仍使用 Trusted Publishing；网络错误或同版本产物不一致会使检查失败。
