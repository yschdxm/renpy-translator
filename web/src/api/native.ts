/** pywebview 原生能力（GUI 模式可用；浏览器模式下全部退回 null/no-op） */

interface PywebviewApi {
  pick_directory(): Promise<string | null>
  pick_zip(): Promise<string | null>
  open_folder(path: string): Promise<void>
}

declare global {
  interface Window {
    pywebview?: { api: PywebviewApi }
  }
}

let readyPromise: Promise<boolean> | null = null

/** 等待 pywebview 注入完成（pywebviewready 事件），超时 2s 视为浏览器模式 */
export function nativeReady(): Promise<boolean> {
  if (window.pywebview) return Promise.resolve(true)
  if (!readyPromise) {
    readyPromise = new Promise((resolve) => {
      const timer = setTimeout(() => resolve(false), 2000)
      window.addEventListener('pywebviewready', () => {
        clearTimeout(timer)
        resolve(true)
      }, { once: true })
    })
  }
  return readyPromise
}

export async function pickDirectory(): Promise<string | null> {
  if (!(await nativeReady())) return null
  return window.pywebview!.api.pick_directory()
}

export async function pickZip(): Promise<string | null> {
  if (!(await nativeReady())) return null
  return window.pywebview!.api.pick_zip()
}

export async function openFolder(path: string): Promise<void> {
  if (!(await nativeReady())) return
  return window.pywebview!.api.open_folder(path)
}

export function isGui(): boolean {
  return !!window.pywebview
}
