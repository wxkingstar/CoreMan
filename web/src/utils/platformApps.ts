import type { PlatformAppIn, PlatformAppOut } from '@/api/types'

/** 列表项 → PUT 请求体：脱敏的密钥原样带回，后端按「未修改」处理。 */
export function toPlatformAppBody(a: PlatformAppOut): PlatformAppIn {
  return {
    platform: a.platform,
    name: a.name,
    capabilities: a.capabilities,
    corp_id: a.corp_id,
    app_id: a.app_id,
    secret: a.secret,
    callback_token: a.callback_token,
    callback_aes_key: a.callback_aes_key,
    extra: a.extra,
    enabled: a.enabled,
  }
}
