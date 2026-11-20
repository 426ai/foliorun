# Foliorun 文档工具镜像源码

构建上下文为仓库根。保留 Dockerfile、OS/Python 锁文件、通用 DOCX 入口、第三方
NOTICE、合成 Skill 和 manifest；它们是镜像源码，不是已发布的镜像。本次没有重建、
注册、上传或部署镜像，也没有真实模型、Temporal 或 OpenSandbox 验收。没有可供拉取的
Foliorun 镜像地址、已发布 tag 或运行镜像 digest。

框架 legacy 默认 capability 仍为 `docx-v1`，本次没有更改它。DOCX 示例绑定处理器版本为
`2`，release identity 为 `generic-docx-2.0.0`。Skill manifest 版本为 `2.0.0`，独立于
Python distribution 版本。`/opt/document-tools` 是工具协议路径，
镜像内不安装 Foliorun Python 包。Dockerfile 中固定的 Python parent digest 仅用于构建
输入，不是 Foliorun 镜像产物。

以下是未来由部署方自行授权的本地构建与检查命令。`foliorun-tools:local` 是操作者创建的
本地标签，不代表远程发布：

```sh
docker build -f image/Dockerfile -t foliorun-tools:local .
docker run --rm --pull never \
  --network none --user 10001:10001 --read-only --cap-drop ALL \
  --security-opt no-new-privileges --cpus 1 --memory 2g \
  --tmpfs /workspace:uid=10001,gid=10001 --tmpfs /output:uid=10001,gid=10001 \
  --tmpfs /tmp:uid=10001,gid=10001 foliorun-tools:local \
  python /opt/document-tools/tool.py selfcheck
```

部署方必须自行构建、校验并获取执行环境可用的镜像 digest，以及 manifest/Skill 字节
hash，再创建对应 profile。构建需要下载固定的上游依赖，不属于离线包验证。

## DOCX 操作

所有示例和文档均为虚构合成内容。`generate` 新建 DOCX，只写入调用方提供的文本，不添加
标题，也不接受模板；章节标题由宿主组装。`revise` 载入 `source` 并在文档末尾追加 `text`。
它能保留现有文档结构，但不提供占位符替换或任意位置插入。以下 revise/slice payload
使用回归测试生成的合成模板；测试会将它导出为 `synthetic-template.docx`。

以下每个 JSON 对象是一次独立调用：

```json
{"operation":"generate","text":"Entirely fictional synthetic body text"}
```

```json
{"operation":"revise","source":"/documents/synthetic-template.docx","text":"Entirely fictional appended paragraph"}
```

```json
{"operation":"slice","source":"/documents/synthetic-template.docx","start":0,"end":2}
```

```json
{"operation":"slice","source":"/documents/synthetic-template.docx","start":1,"end":2,"unit":"block"}
```

`slice` 载入原文档并删去未选中的正文块，保留所选段落/表格 XML 和原文档包。`unit` 为
可选项，默认 `paragraph`：`start`、`end` 是顶层正文段落的零基 `[start,end)`。输出包含
从所选首段到所选末段（含）之间的表格；首段之前和末段之后的表格不包含。`unit="block"`
时，索引按顶层正文段落和表格混合后的文档顺序计算，范围为零基 `[start,end)`，所以可以
只选择一个表格。示例中的 block payload 假定输入文档的第 0 块是段落、第 1 块是表格。

切片拒绝空范围、非整数（包括布尔值）、超出范围的索引和未知 `unit`；只支持单节文档，
正文直接子节点仅支持段落、表格及最终的 `sectPr`。多节文档、段落节属性和其它顶层正文
节点会明确报错。最终 `sectPr`、样式、页眉/页脚、编号、媒体和关系随原文档保留。单节
切片不承诺分页不变，内容变化仍可能导致重新分页；它不是页面精确拆分器或脱敏工具。删除
正文块不会清除文档包中未再引用的媒体/关系、修订记录或其他元数据，原始 `source` 文件
也保持不变。`revise` 和 `slice` 返回新的 `/output` 文件，并沿用原有封存与收据行为。

在旧实现 `a593192401abdeb6e0139729c015960128c1ab09` 中，`generate` 会额外添加
“Entirely fictional synthetic document” 标题；`slice` 会把段落文本复制到空白文档并丢失
结构。这些是旧演示行为；当前示例使用新的工具处理器版本和 release identity。

## 合成 DOCX 回归测试

核心离线套件保持 SDK-only。镜像工具回归测试使用与镜像相同的锁定依赖，显式运行，不会因
缺少 `python-docx` 而跳过：

```sh
mkdir -p "$HOME/.venvs"
uv venv --python 3.12 "$HOME/.venvs/foliorun-image-tool"
uv pip install --python "$HOME/.venvs/foliorun-image-tool/bin/python" \
  --require-hashes -r image/requirements.lock
"$HOME/.venvs/foliorun-image-tool/bin/python" image/tests/test_tool.py
uv run --offline --frozen python tests/offline_runner.py -q
```

如需保留可检查的合成 DOCX 工件，可在新的空目录运行镜像测试：

```sh
FOLIORUN_DOCX_ARTIFACT_DIR="$HOME/foliorun-docx-evidence" \
  "$HOME/.venvs/foliorun-image-tool/bin/python" image/tests/test_tool.py
```

## 镜像边界与检查

构建配置固定 Python parent index、Debian snapshot、直接 OS 包版本与 Python wheel hash。
镜像不含 Pydantic AI、OpenAI、Temporal、OpenSandbox SDK 或业务后端/数据库客户端。SDK
运行需要 execd 的 root bootstrap；文档入口通过 `runuser -u app` 前台执行。generate、
revise、slice、render 接受结构化参数，不接受源码、shell 或运行时安装。submit 前检查
`/output` 普通文件，拒绝符号链接/特殊节点后封存并下载。

Skill manifest 位于 `/opt/document-tools/skills-manifest.json`，profile 固定其字节 hash。
当轮 VFS 的 `/skills` 不授予额外工具权限。wheel 不携带这些 OS 工具；sdist 包含镜像源码
与 Skill，镜像上下文由白名单限定为 `image` 与 `skills`。

dpkg/Python inventory 在未来构建时生成，不是本快照已有的验收记录。
[NOTICE](THIRD_PARTY_NOTICES.md) 说明第三方资产与审阅边界；没有完整 SBOM、公开分发许可、
传递 NOTICE 或对应源码义务审阅完成的声明。
