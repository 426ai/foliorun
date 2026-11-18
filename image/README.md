# Foliorun 文档工具镜像源码

构建上下文为仓库根。保留 Dockerfile、OS/Python 锁文件、通用 DOCX 入口、
第三方 NOTICE、合成 Skill 和 manifest；它们不是已发布的镜像。
本快照没有重建、注册或上传镜像，也没有真实模型、Temporal 或 OpenSandbox 验收。
没有可供拉取的 Foliorun 镜像地址、已发布 tag 或运行镜像 digest。

工具能力为 `docx-v1`，Skill manifest 为 `1.0.0`，独立于 Python distribution 版本。
`/opt/document-tools` 是工具协议路径，镜像内不安装 Foliorun Python 包。
Dockerfile 中固定的 Python parent digest 仅用于构建输入，不是 Foliorun 镜像产物。

以下是未来由部署方自行授权的本地构建与检查命令，此快照准备过程未执行它们。
`foliorun-tools:local` 是操作者创建的本地标签，不代表远程发布：

```sh
docker build -f image/Dockerfile -t foliorun-tools:local .
docker run --rm --pull never \
  --network none --user 10001:10001 --read-only --cap-drop ALL \
  --security-opt no-new-privileges --cpus 1 --memory 2g \
  --tmpfs /workspace:uid=10001,gid=10001 --tmpfs /output:uid=10001,gid=10001 \
  --tmpfs /tmp:uid=10001,gid=10001 foliorun-tools:local \
  python /opt/document-tools/tool.py selfcheck
```

部署方必须自行构建、校验并获取执行环境可用的镜像引用（建议使用 digest），
填入 `Profile.image`，并向示例装配传入 Skill manifest 的字节 SHA-256。构建需要
下载固定的上游依赖，不属于离线包验证。

构建配置固定 Python parent index、Debian snapshot、直接 OS 包版本与 Python wheel hash。
镜像不含 Pydantic AI、OpenAI、Temporal、OpenSandbox SDK 或业务后端/数据库客户端。
SDK 运行需要 execd 的 root bootstrap；文档入口通过 `runuser -u app` 前台执行。
generate/revise/slice/render 接受结构化参数，不接受源码、shell 或运行时安装。
submit 前检查 `/output` 普通文件，拒绝符号链接/特殊节点后封存并下载。

Skill manifest 位于 `/opt/document-tools/skills-manifest.json`；由
`examples/bindings.py` 中的 DOCX 装配显式验证，不是通用 OpenSandbox 协议。
当轮 VFS 的 `/skills` 不授予额外工具权限。wheel 不携带这些 OS 工具；sdist 包含镜像源码
与 Skill，镜像上下文由白名单限定为 image 与 skills。

dpkg/Python inventory 在未来构建时生成，不是本快照已有的验收记录。
[NOTICE](THIRD_PARTY_NOTICES.md) 说明第三方资产与审阅边界；没有完整 SBOM、
公开分发许可、传递 NOTICE 或对应源码义务审阅完成的声明。
