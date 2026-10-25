# 公开内容检查

每次提交前检查代码、注释、文档、测试数据、文件名和完整提交说明。公开内容只描述当前行为、使用方法、必要的技术约束与验证结果。

以下内容不得进入公开仓库或提交说明：

- 凭证、真实账号、资源标识、人员信息、公司名称、内部地址和个人路径。
- 项目迭代计划、发展路线、内部讨论、临时想法及开发过程记录。
- 非必要的其他项目借鉴、学习过程和来源讨论。

保留第三方许可证要求的版权声明、NOTICE 和必要署名，不以内容清理为由删除它们。功能使用中的操作计划、预览和调度功能属于产品行为，可以正常说明。

## 本机安装

```sh
sh scripts/install-git-hooks.sh
```

`pre-commit` 扫描整个暂存区，`commit-msg` 扫描完整提交说明，`pre-push` 检查待推送提交的内容和说明。每次推送检查目标完整快照；新建分支或标签时检查该远端尚未包含的提交。已发布历史用 `--history` 单独审计。推送前先 `git fetch <remote>` 更新远端引用。检查失败会阻止提交，不输出原始命中内容。已有其他 hooks 时，安装器停止并提示合并配置，不覆盖原 hooks。

公司名称、内部域名及其他专有标识逐行写入 `git rev-parse --git-path info/public-content-terms` 指向的本机文件；该文件位于 Git 元数据目录，不提交。规则按不区分大小写的字面子串匹配，使用具有辨识度的完整标识，避免通用词。

## 检查命令

```sh
python3 scripts/check-public-content.py --all
python3 scripts/check-public-content.py --staged
python3 scripts/check-public-content.py --commits origin/main..HEAD
python3 scripts/check-public-content.py --history
```

当前文件通过不代表历史已清理。`--commits` 检查指定范围内每次提交修改的文件与提交说明；`--history` 检查本地所有引用可达的历史版本。不要用包含秘密的实际值来编写扫描器测试。

CI 执行通用检查，不上传本机专有标识库。自动规则无法识别所有语义泄露，提交者仍须人工检查完整 diff 和提交说明；不得用 `--no-verify` 跳过检查。Git 作者姓名与邮箱是单独的公开元数据，使用适合公开的身份。

发现历史问题时先停止推送，确定清理范围后再处理历史；不自动改写已发布提交或强制推送。
