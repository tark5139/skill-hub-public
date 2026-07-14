# GitHub 公开发布治理基线

`tark5139/skill-hub-public` 是经授权的公开分发镜像，不是 Registry 的事实源。仓库中的
源码提交、Pull Request、Issue 或管理员身份，都不能替代某一 Skill 精确版本的公开授权。

每次公开发布必须同时满足：

1. Skill 可见性为 `public`，精确版本已在 Hub 内发布且未弃用；
2. 安全扫描、许可证检查和 Ed25519 验签均通过；
3. `tark5139` 对该版本提交 `PUBLISH_PUBLICLY`，授权记录绑定 artifact SHA-256、manifest
   SHA-256、目标 owner/repository、tag 与审批证据；
4. tag 与 Release 均不存在；发布器创建 Draft、上传并回读校验全部资产后才转为公开；
5. 任一冲突或校验失败均失败关闭，既有 tag、资产或 Release 不得覆盖；
6. macOS 公共二进制必须使用 Developer ID 签名并通过 Apple 公证；`adhoc` 构建只用于本机验收。

仓库保护建议：默认分支要求 CI 通过和 CODEOWNER 审核；禁用强制推送与分支删除；启用秘密扫描、
依赖告警以及 GitHub 提供的 Release immutability。仓库创建后使用以下命令启用并验证：

```sh
gh api --method PUT -H "X-GitHub-Api-Version: 2026-03-10" \
  repos/tark5139/skill-hub-public/immutable-releases
gh api -H "X-GitHub-Api-Version: 2026-03-10" \
  repos/tark5139/skill-hub-public/immutable-releases
```

发布器必须在创建 Draft 前确认 `enabled=true`，并在发布后再次确认 Release 的
`immutable=true`。禁用仓库策略不会解锁已经发布的不可变 Release。

撤销策略不是删除历史资产。发现问题时，应在 Hub 中弃用版本、移除推荐标签、发布安全替代版本，
并在 GitHub Release 说明中增加显著的撤销通知；任何已公开内容都按已被外部复制处理。
