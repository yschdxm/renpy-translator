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

/** 从错误响应体组装 ApiError：优先 error.code/message/detail，
 *  兼容 FastAPI 风格的 detail 字段（字符串或对象数组） */
function toApiError(status: number, statusText: string, data: unknown): ApiError {
  let code = 'HTTP_' + status
  let message = statusText
  let detail = ''
  const body = data as {
    error?: { code?: string; message?: string; detail?: string }
    detail?: unknown
  } | null
  if (body?.error) {
    code = body.error.code ?? code
    message = body.error.message ?? message
    detail = body.error.detail ?? ''
  } else if (body?.detail != null) {
    message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
  }
  return new ApiError(status, code, message, detail)
}

/** 读取 fetch 错误响应体并抛出 ApiError（非 JSON 体退化为 HTTP_状态码） */
async function throwResponseError(resp: Response): Promise<never> {
  let data: unknown = null
  try { data = await resp.json() } catch { /* 非 JSON 错误体 */ }
  throw toApiError(resp.status, resp.statusText, data)
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const resp = await fetch(url, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!resp.ok) await throwResponseError(resp)
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
    if (!resp.ok) await throwResponseError(resp)
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
          reject(toApiError(xhr.status, xhr.statusText, data))
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
