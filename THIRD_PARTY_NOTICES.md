# 第三方代码与资源

以下组件保留各自的版权和许可，不因主项目许可证改变而被替代。

## Runtime Go 驱动

`runtime_daemon/drivers/` 中的 Go 驱动（`relay-claude`、`relay-codex` 及 `pkg/`）基于开源项目 clawrelay-api 修改而来，Go 模块名仍为 `clawrelay-api`。CoreMan 对这些文件的修改同样以 MIT 许可证发布。

- 版权：Copyright (c) 2025 roodkcab。
- 许可证：[MIT 原文](runtime_daemon/drivers/LICENSE)。
- 导入版本（上游 revision）与逐文件摘要：[SOURCE.json](runtime_daemon/drivers/SOURCE.json)。该文件记录导入时的上游快照，不代表后续修改后的文件摘要。
- Runtime 安装包构建会附带 `LICENSE.clawrelay`。

## 模型提供商图标

`web/public/providers/` 使用 LobeHub 的 lobe-icons 资源。

- 版权：Copyright (c) 2023 LobeHub。
- 许可证：[MIT 原文](web/public/providers/LICENSE)。
- 来源与品牌说明：[原始声明](web/public/providers/NOTICE.md)。商标仍属于各权利人，不表示本项目获得官方背书。

## 包依赖

Python、JavaScript 与 Go 依赖分别由 `uv.lock`、`web/package-lock.json` 和 `runtime_daemon/drivers/go.sum` 记录，依赖适用各自许可证。此文件不是完整依赖许可证清单。
