/** API 客户端：统一错误形状（响亮失败——含 detail/traceback 直达 UI） */

export class ApiError extends Error {
  code: string
  detail: string
  status: number

  constructor(status: number, code: string, message: string, detail: string) {
    super(message)
    this.status = status
    this.code = code
    this.detail = detail
  }
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const resp = await fetch(url, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!resp.ok) {
    let code = 'HTTP_' + resp.status
    let message = resp.statusText
    let detail = ''
    try {
      const data = await resp.json()
      if (data?.error) {
        code = data.error.code ?? code
        message = data.error.message ?? message
        detail = data.error.detail ?? ''
      } else if (data?.detail) {
        message = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
      }
    } catch { /* 非 JSON 错误体 */ }
    throw new ApiError(resp.status, code, message, detail)
  }
  return resp.json() as Promise<T>
}

export const api = {
  get: <T>(url: string) => request<T>('GET', url),
  post: <T>(url: string, body?: unknown) => request<T>('POST', url, body),
  put: <T>(url: string, body?: unknown) => request<T>('PUT', url, body),
  patch: <T>(url: string, body?: unknown) => request<T>('PATCH', url, body),
  del: <T>(url: string) => request<T>('DELETE', url),
  /** multipart 表单（文件上传；不能设 Content-Type，浏览器自带 boundary） */
  postForm: async <T>(url: string, form: FormData): Promise<T> => {
    const resp = await fetch(url, { method: 'POST', body: form })
    if (!resp.ok) {
      let code = 'HTTP_' + resp.status
      let message = resp.statusText
      let detail = ''
      try {
        const data = await resp.json()
        if (data?.error) {
          code = data.error.code ?? code
          message = data.error.message ?? message
          detail = data.error.detail ?? ''
        }
      } catch { /* 非 JSON 错误体 */ }
      throw new ApiError(resp.status, code, message, detail)
    }
    return resp.json() as Promise<T>
  },
  /** 带上传进度的 multipart（fetch 不支持上传进度，用 XHR） */
  postFormProgress<T>(url: string, form: FormData,
                      onProgress: (pct: number) => void): Promise<T> {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest()
      xhr.open('POST', url)
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(e.loaded / e.total)
      }
      xhr.onload = () => {
        let data: unknown = null
        try { data = JSON.parse(xhr.responseText) } catch { /* 非 JSON */ }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(data as T)
        } else {
          const err = (data as { error?: { code?: string; message?: string; detail?: string } })?.error
          reject(new ApiError(xhr.status, err?.code ?? 'HTTP_' + xhr.status,
                              err?.message ?? xhr.statusText, err?.detail ?? ''))
        }
      }
      xhr.onerror = () => reject(new ApiError(0, 'NETWORK', '网络错误', ''))
      xhr.send(form)
    })
  },
}

/** 在 message.error 中展示错误（含可折叠 detail） */
export function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.detail ? `${e.message}\n${e.detail}` : e.message
  return String(e)
}
