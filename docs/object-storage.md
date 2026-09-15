# 短期附件存储

默认 `OBJECT_STORAGE=local` 使用共享卷 `/data/storage/objects`。可配置 `OBJECT_STORAGE=s3`，并设置 S3_BUCKET、S3_ACCESS_KEY、S3_SECRET_KEY；S3_REGION 默认 us-east-1，S3_ENDPOINT 可指定 HTTPS 兼容服务，S3_PREFIX 默认 coreman-objects。临时凭据另设 S3_SESSION_TOKEN。配置示例见 `.env.example`。

SDK 使用显式凭据、独立 Session、HTTPS 证书验证，忽略环境代理与外部覆盖的 endpoint；不使用机器元数据凭据链。自定义 endpoint 不允许内嵌凭据、查询串、路径和已知元数据地址。账号应限定独立 bucket/prefix 的 Put/Get/Delete/List；不得将该前缀与其它应用混用。SDK 参考：[PutObject](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/put_object.html)、[GetObject](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/get_object.html)。

上传最大 100 MiB，先写临时文件再上传，校验 Content-MD5，并保存 SHA-256、对象版本及存储配置指纹。取消时等待已开始的同步文件/SDK 操作结束后关闭资源，避免读写已关闭句柄。上传已完成而数据库提交失败可能产生孤儿，不能把事务回滚当作对象已删除。

链接仍为 CoreMan 的 24 小时签名地址。API 校验签名和有效期，再按元数据选择 local 或 S3，作为 attachment/octet-stream/nosniff/no-store 返回。切到 S3 后仍可读取本地旧附件；切回 local 时保留 S3 配置可继续读 S3 旧附件。更换 bucket/endpoint/prefix 不会误读另一个存储：指纹不符明确失败，变更前应等待旧链接过期或单独迁移对象。

scheduler 每轮清理最多 500 个过期元数据对象，S3 批量删除失败保留记录重试。每轮最多扫描一页 1000 个对象，游标推进；只清理专用前缀下 UUID 文件名、无元数据且超过 25 小时的孤儿。其它文件名、前缀和新对象不动。S3 bucket 若启用版本管理，还需配置非当前版本的生命周期规则；SDK 重试产生的历史版本不由普通对象列表完整枚举。应用未修改 bucket 策略或生命周期规则。

自动测试使用 S3 测试替身，不访问真实云 bucket。接入真实 S3 兼容服务前，请在目标环境验证 endpoint、账号权限与版本保留策略。
