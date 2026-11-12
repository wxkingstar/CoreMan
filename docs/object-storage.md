# 短期附件存储

默认 `OBJECT_STORAGE=local` 使用共享卷 `/data/storage/objects`。可配置 `OBJECT_STORAGE=s3`，并设置 S3_BUCKET、S3_ACCESS_KEY、S3_SECRET_KEY；S3_REGION 默认 us-east-1，S3_ENDPOINT 可指定 HTTPS 兼容服务，S3_PREFIX 默认 coreman-objects。临时凭据另设 S3_SESSION_TOKEN。配置示例见 `.env.example`。

SDK 使用显式凭据、独立 Session、HTTPS 证书验证，忽略环境代理与外部覆盖的 endpoint；不使用机器元数据凭据链。自定义 endpoint 不允许内嵌凭据、查询串、路径和已知元数据地址。账号应限定独立 bucket/prefix 的 Put/Get/Delete/List；不得将该前缀与其它应用混用。SDK 参考：[PutObject](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/put_object.html)、[GetObject](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/get_object.html)。

上传最大 100 MiB，先写临时文件再上传，校验 Content-MD5，并保存 SHA-256、对象版本及存储配置指纹。取消时等待已开始的同步文件/SDK 操作结束后关闭资源，避免读写已关闭句柄。上传已完成而数据库提交失败可能产生孤儿，不能把事务回滚当作对象已删除。

链接仍为 CoreMan 的 24 小时签名地址。API 校验签名和有效期，再按元数据选择 local 或 S3，作为 attachment/octet-stream/nosniff/no-store 返回。切到 S3 后仍可读取本地旧附件；切回 local 时保留 S3 配置可继续读 S3 旧附件。更换 bucket/endpoint/prefix 不会误读另一个存储：指纹不符明确失败，变更前应等待旧链接过期或单独迁移对象。

scheduler 每轮清理最多 500 个过期元数据对象，S3 批量删除失败保留记录重试。每轮最多扫描一页 1000 个对象，游标推进；只清理专用前缀下 UUID 文件名、无元数据且超过 25 小时的孤儿。其它文件名、前缀和新对象不动。S3 bucket 若启用版本管理，还需配置非当前版本的生命周期规则；SDK 重试产生的历史版本不由普通对象列表完整枚举。应用未修改 bucket 策略或生命周期规则。

自动测试使用 S3 测试替身，不访问真实云 bucket。接入真实 S3 兼容服务前，请在目标环境验证 endpoint、账号权限与版本保留策略。

## 个人助理的临时桶

个人助理带文件的写操作（发文件、存入云空间、带附件的邮件）要等所有者在审批系统里批准，最长 72 小时，批准后还有 24 小时的执行窗口；上面的短期附件一小时就过期，等不到。这期间文件放在一个**单独的** S3 兼容桶里，与 `OBJECT_STORAGE` / `S3_*` 互不相干：

| 设置项 | 说明 |
|---|---|
| `ASSISTANT_TMP_S3_BUCKET`、`ASSISTANT_TMP_S3_ACCESS_KEY`、`ASSISTANT_TMP_S3_SECRET_KEY` | 必填，三项要一起配；只配一部分时进程启动失败 |
| `ASSISTANT_TMP_S3_ENDPOINT` | HTTPS 地址，不带路径、查询串与内嵌凭据；不填时用 SDK 按区域推出的默认端点 |
| `ASSISTANT_TMP_S3_REGION` | 默认 `us-east-1` |
| `ASSISTANT_TMP_S3_PREFIX` | 对象名前缀，默认 `assistant-approvals` |
| `ASSISTANT_TMP_S3_ADDRESSING` | `virtual`（默认，桶名在域名里）或 `path`；按存储服务的要求选 |

部署方必须做的两件事：

1. **桶设为私有**（不允许匿名读、不允许列举），凭据只给这个桶（或这个前缀）的 Put / Get / Delete。
2. **在桶上配置自动清理规则，对象最多保留 7 天。** 平台会在审批单有了结论后尽力删除对象，但删除失败、数据密钥已销毁等情况下只能靠这条规则；7 天也是审批单上查看链接的最长有效期。

安全取舍（请在启用前确认可以接受）：文件本体**不加密**。保护来自三点——桶私有；对象名是 256 位随机串，不带文件名、所有者或助理的任何标识；对象名只保存在用所有者数据密钥加密的审批流水里，以及发给所有者本人的审批单上的预签名链接里。只拿到平台数据库或备份的人定位不到文件；**持有这个桶凭据的人可以列举并读取桶里的文件**，拿到审批单上那条链接的人在链接失效前也可以下载。

批准后平台从桶里取回文件，核对大小与 SHA-256（对不上或取不到都不执行），再以所有者身份上传给飞书。自动测试使用内存里的替身；接入真实的存储服务前，请在目标环境验证端点、寻址方式、预签名下载（含 `response-content-disposition`）与自动清理规则。
