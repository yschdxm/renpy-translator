/** 会话 store：当前项目 + 统计 + 活跃任务 */
import { defineStore } from 'pinia'
import { api } from '../api/client'

export interface CountStat {
  total: number
  translated: number
  untranslated: number
}

export interface SessionStats {
  dialogue?: CountStat
  ui?: CountStat
  names?: CountStat
}

export const useSessionStore = defineStore('session', {
  state: () => ({
    currentProject: '' as string,
    hasTranslator: false,
    stats: {} as SessionStats,
    interruptedJobs: 0,
    loaded: false,
  }),
  getters: {
    progressText(state): string {
      // 只显示有内容的类别；都没有则空
      const parts: string[] = []
      const d = state.stats.dialogue
      const u = state.stats.ui
      const n = state.stats.names
      if (d?.total) parts.push(`对话 ${d.translated}/${d.total}`)
      if (u?.total) parts.push(`字符串 ${u.translated}/${u.total}`)
      if (n?.total) parts.push(`人物 ${n.translated}/${n.total}`)
      return parts.length ? parts.join(' · ') : ''
    },
  },
  actions: {
    async refresh() {
      const data = await api.get<{
        current_project: string
        has_translator: boolean
        stats: SessionStats
        interrupted_jobs?: number
      }>('/api/session')
      this.currentProject = data.current_project
      this.hasTranslator = data.has_translator
      this.stats = data.stats
      this.interruptedJobs = data.interrupted_jobs ?? 0
      this.loaded = true
    },
    async open(name: string) {
      const data = await api.post<{
        current_project: string
        has_translator: boolean
        stats: SessionStats
      }>('/api/session/open', { name })
      this.currentProject = data.current_project
      this.hasTranslator = data.has_translator
      this.stats = data.stats
    },
    async close() {
      await api.post('/api/session/close')
      this.currentProject = ''
      this.stats = {}
    },
  },
})
