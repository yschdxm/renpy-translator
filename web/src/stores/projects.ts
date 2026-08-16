/** 项目列表 store：头部切换器与项目页共用 */
import { defineStore } from 'pinia'
import { api } from '../api/client'

export interface ProjectItem {
  name: string
  game_dir: string
  total_dialogues: number
  translated_dialogues: number
  total_strings: number
  translated_strings: number
  updated_at: string
  progress_percent: number
  progress_text: string
  is_current: boolean
}

export const useProjectsStore = defineStore('projects', {
  state: () => ({
    list: [] as ProjectItem[],
  }),
  actions: {
    async refresh() {
      this.list = await api.get<ProjectItem[]>('/api/projects')
    },
  },
})
