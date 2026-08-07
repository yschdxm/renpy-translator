/** 任务 store：活跃任务跟踪 + SSE 通道（刷新后自动重连回放） */
import { defineStore } from 'pinia'
import { api } from '../api/client'

export interface JobView {
  id: string
  kind: string
  label: string
  status: string
  progress: number
  stage: string
  payload: Record<string, unknown> | null
  question: { question_id: string; type: string; payload: Record<string, unknown> } | null
  result: Record<string, unknown> | null
  error: string | null
  logs: string[]
  /** 前端本地状态 */
  dialogOpen: boolean
  sseFailed: boolean
  /** 已发取消请求、等待任务到检查点（协作式取消，非卡住） */
  cancelling: boolean
}

interface JobRecord {
  id: string
  kind: string
  label: string
  status: string
  progress: number
  stage: string
  payload: Record<string, unknown> | null
  question: JobView['question']
  result: JobView['result']
  error: string | null
}

const TERMINAL = ['succeeded', 'failed', 'cancelled', 'interrupted']

export const useJobsStore = defineStore('jobs', {
  state: () => ({
    jobs: new Map<string, JobView>(),
    channels: new Map<string, EventSource>(),
  }),
  getters: {
    activeJobs(state): JobView[] {
      return [...state.jobs.values()].filter((j) => !TERMINAL.includes(j.status))
    },
  },
  actions: {
    /** 应用启动时：拉取活跃任务并接管（刷新恢复入口）。
     *  只接管真正在跑/等输入的——interrupted 是历史终态，不弹窗。
     *  刷新后不自动打开对话框，由顶栏任务列表手动找回。 */
    async restore() {
      const records = await api.get<JobRecord[]>('/api/jobs?active=1')
      for (const rec of records) {
        if (rec.status === 'running' || rec.status === 'waiting_input') {
          this.track(rec.id, rec, false)
        }
      }
    },

    /** 跟踪任务：建立 SSE 通道。
     *  open=true 时打开进度对话框（并关闭其他对话框，避免多个叠加）；
     *  open=false 仅后台跟踪（页面刷新恢复用）。
     *  未传 initial 时先拉任务记录（补全 kind/label/payload——
     *  页面的按类刷新依赖 kind） */
    track(jobId: string, initial?: Partial<JobRecord>, open = true) {
      if (!this.jobs.has(jobId)) {
        this.jobs.set(jobId, {
          id: jobId,
          kind: initial?.kind ?? '',
          label: initial?.label ?? '',
          status: initial?.status ?? 'running',
          progress: initial?.progress ?? 0,
          stage: initial?.stage ?? '',
          payload: initial?.payload ?? null,
          question: initial?.question ?? null,
          result: initial?.result ?? null,
          error: initial?.error ?? null,
          logs: [],
          dialogOpen: false,
          sseFailed: false,
          cancelling: false,
        })
      }
      const view = this.jobs.get(jobId)!
      if (open) this.openDialog(jobId)
      if (initial) {
        Object.assign(view, {
          kind: initial.kind ?? view.kind,
          label: initial.label ?? view.label,
          status: initial.status ?? view.status,
          progress: initial.progress ?? view.progress,
          stage: initial.stage ?? view.stage,
          payload: initial.payload ?? view.payload,
          question: initial.question ?? view.question,
          result: initial.result ?? view.result,
          error: initial.error ?? view.error,
        })
      } else if (!view.kind) {
        // 补全任务元信息（kind 决定页面自动刷新分组）
        api.get<JobRecord>(`/api/jobs/${jobId}`).then((rec) => {
          view.kind = rec.kind
          view.label = rec.label
          view.payload = rec.payload
        }).catch(() => { /* 记录拉取失败不影响事件流 */ })
      }
      if (!TERMINAL.includes(view.status)) {
        this._connect(jobId, 0)
      }
    },

    _connect(jobId: string, afterSeq: number, attempt = 0) {
      this.channels.get(jobId)?.close()
      const view = this.jobs.get(jobId)
      if (!view) return
      const es = new EventSource(`/api/jobs/${jobId}/events?after_seq=${afterSeq}`)
      this.channels.set(jobId, es)
      let lastSeq = afterSeq

      const onEvent = (kind: string) => (ev: MessageEvent) => {
        const data = JSON.parse(ev.data)
        lastSeq = data.seq ?? lastSeq
        if (kind === 'log') {
          view.logs.push(data.text)
          if (view.logs.length > 2000) view.logs.splice(0, 500)
        } else if (kind === 'progress') {
          view.progress = data.value
          view.stage = data.text || view.stage
        } else if (kind === 'stage') {
          view.stage = data.stage
        } else if (kind === 'question') {
          view.status = 'waiting_input'
          view.question = { question_id: data.question_id, type: data.type, payload: data.payload }
        } else if (kind === 'answered') {
          view.status = 'running'
          view.question = null
        } else if (kind === 'status') {
          view.status = data.status
          if (data.error) view.error = data.detail ? `${data.error}\n${data.detail}` : data.error
          if (data.result) view.result = data.result
          es.close()
          this.channels.delete(jobId)
        }
      }
      for (const kind of ['log', 'progress', 'stage', 'question', 'answered', 'status']) {
        es.addEventListener(kind, onEvent(kind))
      }

      es.onerror = () => {
        es.close()
        this.channels.delete(jobId)
        // SSE 断流：退避重连（after_seq 从 db 回放补齐）；连续失败 → 响亮报错
        const view2 = this.jobs.get(jobId)
        if (!view2 || TERMINAL.includes(view2.status)) return
        if (attempt >= 4) {
          view2.sseFailed = true
          return
        }
        setTimeout(() => this._connect(jobId, lastSeq, attempt + 1),
                   500 * 2 ** attempt)
      }
    },

    /** SSE 连续失败后的手动重试 */
    retry(jobId: string) {
      const view = this.jobs.get(jobId)
      if (!view) return
      view.sseFailed = false
      this._connect(jobId, 0)
    },

    async answer(jobId: string, questionId: string, answer: Record<string, unknown>) {
      await api.post(`/api/jobs/${jobId}/answer`, { question_id: questionId, answer })
    },

    async cancel(jobId: string) {
      const view = this.jobs.get(jobId)
      if (view) view.cancelling = true
      try {
        await api.post(`/api/jobs/${jobId}/cancel`)
      } catch (e) {
        if (view) view.cancelling = false
        throw e
      }
    },

    /** 打开指定任务的对话框，同时关闭其他（同一时刻只开一个） */
    openDialog(jobId: string) {
      for (const j of this.jobs.values()) {
        j.dialogOpen = j.id === jobId
      }
    },

    closeDialog(jobId: string) {
      const view = this.jobs.get(jobId)
      if (view) view.dialogOpen = false
    },

    /** 清理已终结且关闭对话框的任务视图 */
    dismiss(jobId: string) {
      this.channels.get(jobId)?.close()
      this.channels.delete(jobId)
      this.jobs.delete(jobId)
    },
  },
})
