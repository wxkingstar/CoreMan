export type RuntimeDevice = 'mac-studio' | 'mac-mini' | 'server'

/** 按运行时名称猜机型：忽略大小写和分隔符（MacStudio、Mac-Studio、mac_studio 都算），其余一律按服务器。 */
export function runtimeDevice(name: string | null | undefined): RuntimeDevice {
  const n = (name ?? '').toLowerCase().replace(/[^a-z0-9]/g, '')
  if (n.includes('macstudio')) return 'mac-studio'
  if (n.includes('macmini')) return 'mac-mini'
  return 'server'
}
