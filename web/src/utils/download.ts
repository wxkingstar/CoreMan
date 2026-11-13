/** 让浏览器把一段二进制存成文件。 */
export function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = name
  link.click()
  // 立刻回收地址有的浏览器会取消下载：稍等一下。
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
